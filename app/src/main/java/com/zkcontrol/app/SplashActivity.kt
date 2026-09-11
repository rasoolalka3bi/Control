package com.zkcontrol.app

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.appcompat.app.AppCompatActivity

class SplashActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        Thread.setDefaultUncaughtExceptionHandler(
            CrashHandler(applicationContext, Thread.getDefaultUncaughtExceptionHandler())
        )
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_splash)

        Handler(Looper.getMainLooper()).postDelayed({
            val target = if (Prefs.isAppLockEnabled(this)) LockActivity::class.java else MainActivity::class.java
            startActivity(Intent(this, target))
            finish()
        }, 1200)
    }
}
