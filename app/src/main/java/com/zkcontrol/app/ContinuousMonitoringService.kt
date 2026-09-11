package com.zkcontrol.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * "وضع المراقبة المستمرة": خدمة أمامية حقيقية بحلقة تنفيذ داخلية دقيقة (كل
 * دقيقة تقريبًا)، وهي المسؤول الحصري عن تنفيذ جدولات "فترة التكرار" بدقة
 * (بخلاف الأوقات الثابتة التي تبقى دائمًا على WorkManager العادي بشكل
 * مستقل تمامًا) وعن كشف انحراف الوقت في الخلفية.
 *
 * لا تفتح الخدمة اتصالاً منفصلاً ببايثون (Chaquopy) من مسارها الخاص - كان
 * هذا يسبب تعطّلاً كاملاً وفوريًا للتطبيق. بدلاً من ذلك، تتواصل مع خادم
 * Flask المحلي الذي يعمل بالفعل عبر طلب شبكة عادي.
 */
class ContinuousMonitoringService : Service() {

    private val exceptionHandler = CoroutineExceptionHandler { _, _ ->
        isRunning = false
    }
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob() + exceptionHandler)
    private var loopJob: Job? = null

    companion object {
        const val CHANNEL_ID = "zk_continuous_monitoring"
        const val NOTIF_ID = 3001
        const val TICK_INTERVAL_MS = 60_000L // كل دقيقة تقريبًا
        const val ACTION_REFRESH_NOW = "com.zkcontrol.app.REFRESH_NOW"

        @Volatile
        var isRunning = false
    }

    override fun onCreate() {
        super.onCreate()
        try {
            ensureChannel()
        } catch (e: Exception) {
            // لا نسمح لأي فشل هنا بإسقاط العملية بأكملها
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_REFRESH_NOW) {
            // طلب تحديث فوري (بعد مزامنة يدوية من داخل التطبيق) - لا يعيد
            // تشغيل الخدمة، فقط ينفّذ نبضة واحدة إضافية فورًا خارج الحلقة
            // الدورية العادية، بدون انتظار الدقيقة القادمة
            if (isRunning) {
                scope.launch { runOneTick() }
            }
            return START_STICKY
        }

        isRunning = true

        try {
            startForeground(NOTIF_ID, buildSimpleNotification("جارِ التشغيل في الخلفية..."))
        } catch (e: Exception) {
            isRunning = false
            Prefs.setContinuousMonitoringEnabled(applicationContext, false)
            stopSelf()
            return START_NOT_STICKY
        }

        if (loopJob == null || loopJob?.isActive != true) {
            loopJob = scope.launch { runLoop() }
        }
        return START_STICKY
    }

    /** طلب HTTP بسيط لخادم Flask المحلي (بدون أي مكتبات إضافية). */
    private fun httpGet(urlStr: String): String {
        val connection = URL(urlStr).openConnection() as HttpURLConnection
        connection.requestMethod = "GET"
        connection.connectTimeout = 8_000
        connection.readTimeout = 90_000 // قد تأخذ المزامنة وقتًا حسب عدد الأجهزة
        return try {
            connection.inputStream.bufferedReader().use { it.readText() }
        } finally {
            connection.disconnect()
        }
    }

    /** نبضة واحدة: تنفّذ الجدولات المستحقة + كشف الانحراف + تحديث الإشعار.
     * تُستدعى من الحلقة الدورية العادية، ومن طلبات التحديث الفوري كذلك. */
    private suspend fun runOneTick() {
        try {
            val resultJson = httpGet("http://127.0.0.1:5000/api/internal/precise_tick")
            val obj = JSONObject(resultJson)

            val alerts = obj.optJSONArray("alerts")
            if (alerts != null) {
                for (i in 0 until alerts.length()) {
                    try {
                        NotificationHelper.postAlert(applicationContext, alerts.getString(i))
                    } catch (e: Exception) {
                        // تجاهل فشل إشعار واحد ولا نوقف الحلقة كلها
                    }
                }
            }

            val summary = obj.optJSONObject("summary")
            val text = summaryToText(summary)
            withContext(Dispatchers.Main) {
                try {
                    updateNotification(text)
                } catch (e: Exception) {
                    // فشل تحديث الإشعار وحده لا يوقف الحلقة
                }
            }
        } catch (e: Exception) {
            // تجاهل هذه النبضة (غالبًا لأن خادم Flask لم يكتمل إقلاعه بعد
            // أو انقطع الاتصال بأحد الأجهزة) وحاول مرة أخرى في النبضة القادمة
        }
    }

    private suspend fun runLoop() {
        while (loopJob?.isActive == true) {
            runOneTick()
            delay(TICK_INTERVAL_MS)
        }
    }

    /** يبني نص الإشعار بناءً على حالة التزامن الفعلية (وليس مجرد الاتصال)،
     * ويذكر أسماء الأجهزة تحديدًا كلما كان ذلك ممكنًا ومفيدًا:
     * - جهاز واحد فقط: يُذكر اسمه مباشرة
     * - عدة أجهزة، الكل متزامن: ملخّص عام
     * - عدة أجهزة، عدد قليل (1-3) بحاجة لمزامنة: تُذكر أسماؤها تحديدًا
     * - أكثر من 3 بحاجة لمزامنة: يُذكر العدد فقط تفاديًا لسطر طويل جدًا */
    /** يبني نص الإشعار بناءً على حالة التزامن الفعلية - "غير متصل" و"منحرف"
     * حالتان منفصلتان تمامًا (جهاز غير متصل لا يُعتبر متزامنًا أبدًا، حتى لو
     * لم يكن مسجَّلاً كمنحرف - لا يمكن التأكد من وقته وهو غير متصل). تذكر
     * أسماء الأجهزة تحديدًا كلما كان ذلك مفيدًا:
     * - مشكلة واحدة بالضبط (بغض النظر عن العدد الكلي): تُذكر باسمها
     *   ("⚠️ جهاز (الاسم) غير متصل")
     * - 2-3 مشاكل: تُذكر الأسماء مجمّعة حسب النوع (غير متصل / يحتاج مزامنة)
     * - أكثر من 3: يُذكر العدد فقط تفاديًا لسطر طويل جدًا */
    /** يبني نص الإشعار بحيث يذكر حالة كل جهاز صراحة (بما في ذلك الأجهزة
     * السليمة) عند وجود 3 أجهزة أو أقل - حتى لا يبدو الإشعار وكأنه "نسي"
     * ذكر جهاز لم تكن به مشكلة. "غير متصل" و"متصل لكن غير متزامن" حالتان
     * منفصلتان تمامًا وواضحتان لكل جهاز:
     *   "جهاز الموظفين غير متصل"
     *   "جهاز المسؤولين متصل لكن غير متزامن"
     *   "جهاز الفرع الرئيسي متزامن"
     * لأكثر من 3 أجهزة، لا يمكن ذكر الكل بوضوح في سطر واحد، فنكتفي بذكر
     * المشاكل فقط (بالاسم إن كانت مشكلة واحدة، أو بالعدد إن كانت أكثر). */
    private fun summaryToText(summary: JSONObject?): String {
        if (summary == null) return "جارِ التحديث..."
        val total = summary.optInt("total", 0)
        if (total == 0) return "لا توجد أجهزة مضافة"

        val offlineNames = jsonArrayToStringList(summary.optJSONArray("offline_names")).toSet()
        val driftedNames = jsonArrayToStringList(summary.optJSONArray("drifted_names")).toSet()
        val hasProblem = offlineNames.isNotEmpty() || driftedNames.isNotEmpty()

        if (total <= 3) {
            val devicesArr = summary.optJSONArray("devices")
            val allNames = mutableListOf<String>()
            if (devicesArr != null) {
                for (i in 0 until devicesArr.length()) {
                    val name = devicesArr.optJSONObject(i)?.optString("name")
                    if (!name.isNullOrEmpty()) allNames.add(name)
                }
            }
            val sentences = allNames.map { name ->
                when {
                    offlineNames.contains(name) -> "جهاز $name غير متصل"
                    driftedNames.contains(name) -> "جهاز $name متصل لكن غير متزامن"
                    else -> "جهاز $name متزامن"
                }
            }
            val prefix = if (hasProblem) "⚠️ " else "✓ "
            return prefix + sentences.joinToString(" — ")
        }

        // أكثر من 3 أجهزة - نركّز على المشاكل فقط تفاديًا لسطر طويل جدًا
        if (!hasProblem) return "✓ كل الأجهزة متزامنة"

        val problemCount = offlineNames.size + driftedNames.size
        if (problemCount == 1) {
            val isOffline = offlineNames.isNotEmpty()
            val name = if (isOffline) offlineNames.first() else driftedNames.first()
            return if (isOffline) "⚠️ جهاز $name غير متصل" else "⚠️ جهاز $name متصل لكن غير متزامن"
        }

        val parts = mutableListOf<String>()
        if (offlineNames.isNotEmpty()) parts.add("${offlineNames.size} غير متصل")
        if (driftedNames.isNotEmpty()) parts.add("${driftedNames.size} غير متزامن")
        return "⚠️ " + parts.joinToString("، ")
    }

    private fun jsonArrayToStringList(arr: org.json.JSONArray?): List<String> {
        if (arr == null) return emptyList()
        val list = mutableListOf<String>()
        for (i in 0 until arr.length()) list.add(arr.getString(i))
        return list
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(CHANNEL_ID) != null) return

            val channel = NotificationChannel(
                CHANNEL_ID, "وضع المراقبة المستمرة", NotificationManager.IMPORTANCE_LOW
            )
            channel.description = "إشعار هادئ يعرض حالة تزامن الأجهزة أثناء المراقبة الدقيقة في الخلفية"
            channel.setSound(null, null)
            channel.enableVibration(false)
            channel.setShowBadge(false)
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildSimpleNotification(text: String): android.app.Notification {
        val openIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val openPending = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_notify)
            .setContentText(text)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setSilent(true)
            .setOngoing(true)
            .setContentIntent(openPending)
            .build()
    }

    private fun updateNotification(text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIF_ID, buildSimpleNotification(text))
    }

    override fun onDestroy() {
        isRunning = false
        loopJob?.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
