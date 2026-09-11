package com.zkcontrol.app

import android.content.Context

object Prefs {
    private const val FILE = "zk_native_prefs"

    fun isAppLockEnabled(context: Context): Boolean =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE).getBoolean("app_lock_enabled", false)

    fun setAppLockEnabled(context: Context, enabled: Boolean) {
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().putBoolean("app_lock_enabled", enabled).apply()
    }

    fun isContinuousMonitoringEnabled(context: Context): Boolean =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE).getBoolean("continuous_monitoring_enabled", false)

    fun setContinuousMonitoringEnabled(context: Context, enabled: Boolean) {
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().putBoolean("continuous_monitoring_enabled", enabled).apply()
    }
}
