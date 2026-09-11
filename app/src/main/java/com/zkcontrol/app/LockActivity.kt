package com.zkcontrol.app

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat

class LockActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_lock)

        val statusText = findViewById<TextView>(R.id.lockStatusText)
        val retryButton = findViewById<Button>(R.id.retryButton)

        val biometricManager = BiometricManager.from(this)
        val canAuth = biometricManager.canAuthenticate(
            BiometricManager.Authenticators.BIOMETRIC_WEAK or BiometricManager.Authenticators.DEVICE_CREDENTIAL
        )

        if (canAuth != BiometricManager.BIOMETRIC_SUCCESS) {
            // لا توجد بصمة/قفل شاشة مُعدّ على الجهاز، نسمح بالدخول مباشرة لتفادي حبس المستخدم خارج التطبيق
            proceedToApp()
            return
        }

        retryButton.setOnClickListener { showPrompt() }
        showPrompt()
    }

    private fun showPrompt() {
        val executor = ContextCompat.getMainExecutor(this)
        val prompt = BiometricPrompt(this, executor, object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                proceedToApp()
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                findViewById<TextView>(R.id.lockStatusText).text = "فشل التحقق: $errString"
            }

            override fun onAuthenticationFailed() {
                findViewById<TextView>(R.id.lockStatusText).text = "لم يتم التعرف على البصمة، حاول مرة أخرى"
            }
        })

        val promptInfo = BiometricPrompt.PromptInfo.Builder()
            .setTitle("فتح التطبيق")
            .setSubtitle("استخدم بصمتك أو قفل الشاشة للمتابعة")
            .setAllowedAuthenticators(
                BiometricManager.Authenticators.BIOMETRIC_WEAK or BiometricManager.Authenticators.DEVICE_CREDENTIAL
            )
            .build()

        prompt.authenticate(promptInfo)
    }

    private fun proceedToApp() {
        startActivity(Intent(this, MainActivity::class.java))
        finish()
    }
}
