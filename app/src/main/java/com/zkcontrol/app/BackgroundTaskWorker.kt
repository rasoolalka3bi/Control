package com.zkcontrol.app

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray

/**
 * ينفّذ مزامنة "وقت ثابت" واحد محدد عند حلول موعده بالضبط، ثم يعيد جدولة
 * نفسه لليوم التالي. هذه هي المهمة الخلفية الوحيدة المتبقية على WorkManager
 * العادي - فترة التكرار وكشف انحراف الوقت في الخلفية أصبحا حصريًا من
 * مسؤولية ContinuousMonitoringService.
 */
class BackgroundTaskWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val scheduleId = inputData.getString("schedule_id")
        val time = inputData.getString("time")

        return try {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(applicationContext))
            }
            val py = Python.getInstance()
            val appModule = py.getModule("app")

            val resultJson = appModule.callAttr(
                "run_fixed_time_sync", applicationContext.filesDir.absolutePath, scheduleId, time
            ).toString()
            postAlertsFromJson(resultJson)

            if (scheduleId != null && time != null) {
                Scheduler.scheduleNextFixedRun(applicationContext, scheduleId, time)
            }
            Result.success()
        } catch (e: Exception) {
            // حتى عند الفشل، نعيد الجدولة لليوم التالي حتى لا تتوقف الجدولة نهائيًا
            if (scheduleId != null && time != null) {
                Scheduler.scheduleNextFixedRun(applicationContext, scheduleId, time)
            }
            Result.failure()
        }
    }

    private fun postAlertsFromJson(json: String) {
        try {
            val arr = JSONArray(json)
            for (i in 0 until arr.length()) {
                NotificationHelper.postAlert(applicationContext, arr.getString(i))
            }
        } catch (e: Exception) {
            // تجاهل - لا توجد تنبيهات جديدة أو استجابة غير متوقعة
        }
    }
}
