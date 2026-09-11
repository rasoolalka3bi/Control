package com.zkcontrol.app

import android.content.Context
import androidx.work.*
import org.json.JSONArray
import java.io.File
import java.util.Calendar
import java.util.concurrent.TimeUnit

/**
 * مسؤول حصريًا عن جدولة "الأوقات الثابتة" عبر WorkManager العادي، وهي
 * الوحيدة التي تبقى تعمل بهذا الأسلوب بغض النظر عن حالة "وضع المراقبة
 * المستمرة" (لأنها دقيقة أصلاً بما يكفي بدون الحاجة لخدمة أمامية).
 *
 * أما "فترة التكرار" وكشف انحراف الوقت في الخلفية، فأصبحا حصريًا من
 * مسؤولية ContinuousMonitoringService وحدها (حلقة داخلية دقيقة عبر
 * run_precise_tick) - لا وجود لأي "نبضة موحّدة تقريبية" بديلة لهما هنا.
 */
object Scheduler {

    private const val TAG_FIXED = "zk_sync_fixed"

    private fun readJsonArray(context: Context, name: String): JSONArray {
        return try {
            val f = File(context.filesDir, name)
            if (!f.exists()) JSONArray() else JSONArray(f.readText())
        } catch (e: Exception) {
            JSONArray()
        }
    }

    /** يُعاد استدعاؤها بعد أي تغيير في مجموعات الجدولة - تقرأ schedule.json
     * (مصفوفة من المجموعات) مباشرة وتعيد جدولة كل الأوقات الثابتة فقط. */
    fun refreshBackgroundSchedule(context: Context) {
        val schedules = readJsonArray(context, "schedule.json")
        val wm = WorkManager.getInstance(context)

        wm.cancelAllWorkByTag(TAG_FIXED)

        for (i in 0 until schedules.length()) {
            val sched = schedules.getJSONObject(i)
            if (!sched.optBoolean("enabled", true)) continue
            if (!sched.optBoolean("use_times", false)) continue

            val scheduleId = sched.optString("id")
            if (scheduleId.isNullOrEmpty()) continue

            val times = sched.optJSONArray("times")
            if (times != null) {
                for (j in 0 until times.length()) {
                    scheduleNextFixedRun(context, scheduleId, times.getString(j))
                }
            }
        }
    }

    /** يجدول تنفيذًا واحدًا عند أقرب حدوث قادم لهذا الوقت (اليوم أو غدًا)
     * لمجموعة جدولة محددة - والـ Worker نفسه يعيد جدولة اليوم التالي
     * بعد التنفيذ. */
    fun scheduleNextFixedRun(context: Context, scheduleId: String, timeStr: String) {
        val parts = timeStr.split(":")
        if (parts.size != 2) return
        val hour = parts[0].toIntOrNull() ?: return
        val minute = parts[1].toIntOrNull() ?: return

        val now = Calendar.getInstance()
        val target = Calendar.getInstance()
        target.set(Calendar.HOUR_OF_DAY, hour)
        target.set(Calendar.MINUTE, minute)
        target.set(Calendar.SECOND, 0)
        target.set(Calendar.MILLISECOND, 0)
        if (target.before(now) || target == now) {
            target.add(Calendar.DAY_OF_MONTH, 1)
        }
        val delayMs = target.timeInMillis - now.timeInMillis

        val request = OneTimeWorkRequestBuilder<BackgroundTaskWorker>()
            .setInitialDelay(delayMs, TimeUnit.MILLISECONDS)
            .addTag(TAG_FIXED)
            .setInputData(workDataOf("mode" to "fixed", "schedule_id" to scheduleId, "time" to timeStr))
            .build()

        WorkManager.getInstance(context).enqueueUniqueWork(
            "zk_sync_fixed_${scheduleId}_$timeStr",
            ExistingWorkPolicy.REPLACE,
            request
        )
    }
}
