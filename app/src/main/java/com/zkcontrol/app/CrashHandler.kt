package com.zkcontrol.app

import android.content.Context
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * يسجّل أي عطل غير متوقع في ملف نصي يمكن الوصول إليه من أي مدير ملفات
 * عادي (بدون حاجة لـ adb أو logcat)، في:
 * Android/data/com.zkcontrol.app/files/crash_log.txt
 * يُستخدم فقط للتشخيص أثناء التطوير، ولا يمنع حدوث العطل نفسه - فقط
 * يسجّله قبل أن يتابع النظام سلوكه الطبيعي (إغلاق التطبيق).
 */
class CrashHandler(
    private val context: Context,
    private val defaultHandler: Thread.UncaughtExceptionHandler?
) : Thread.UncaughtExceptionHandler {

    override fun uncaughtException(thread: Thread, throwable: Throwable) {
        try {
            val sw = StringWriter()
            throwable.printStackTrace(PrintWriter(sw))
            val timestamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()).format(Date())

            val dir = context.getExternalFilesDir(null)
            if (dir != null) {
                val file = File(dir, "crash_log.txt")
                file.appendText("\n===== عطل جديد في $timestamp =====\n$sw\n")
            }
        } catch (e: Exception) {
            // تجاهل - لا نريد لخطأ في التسجيل نفسه أن يعطّل التعامل مع العطل الأصلي
        }
        defaultHandler?.uncaughtException(thread, throwable)
    }
}
