package com.zkcontrol.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

object NotificationHelper {
    private const val CHANNEL_ID = "zk_background_alerts"
    private var nextId = 2000

    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = context.getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(CHANNEL_ID) != null) return

            val channel = NotificationChannel(
                CHANNEL_ID, "تنبيهات إدارة أجهزة البصمة", NotificationManager.IMPORTANCE_HIGH
            )
            channel.description = "تنبيهات فشل المزامنة، انقطاع الاتصال، وتغيير الوقت غير المتوقع"
            channel.enableVibration(true)
            val soundAttrs = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build()
            channel.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION), soundAttrs)
            manager.createNotificationChannel(channel)
        }
    }

    /** يعرض إشعار نظام حقيقي في شريط الحالة (بالإضافة إلى الحفظ داخل جرس التطبيق،
     * الذي يتم بالفعل من جانب بايثون عبر notifications.json). */
    fun postAlert(context: Context, message: String) {
        ensureChannel(context)

        val builder = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)

        try {
            NotificationManagerCompat.from(context).notify(nextId++, builder.build())
        } catch (e: SecurityException) {
            // المستخدم لم يمنح إذن الإشعارات (Android 13+) - نتجاهل بصمت،
            // الإشعار محفوظ بالفعل داخل جرس التطبيق كبديل
        }
    }
}
