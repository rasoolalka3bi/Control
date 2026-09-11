package com.zkcontrol.app

import android.Manifest
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.ContentValues
import android.content.pm.PackageManager
import android.media.MediaScannerConnection
import android.os.Environment
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.MediaStore
import android.provider.Settings
import android.view.View
import android.view.animation.AlphaAnimation
import android.webkit.JavascriptInterface
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var loadingOverlay: View
    private val pendingDownloadIds = mutableSetOf<Long>()

    /** رد اختيار الملف المعلّق من الواجهة (input type="file") - بدون هذا
     * لا يفتح WebView أي نافذة لاختيار الملفات إطلاقًا. */
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null

    private val fileChooserLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val callback = fileChooserCallback ?: return@registerForActivityResult
            fileChooserCallback = null
            callback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
        }

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* لا حاجة لإجراء إضافي */ }

    private val downloadCompleteReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1)
            if (id == -1L || !pendingDownloadIds.contains(id)) return
            pendingDownloadIds.remove(id)
            shareDownloadedFile(id)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webview)
        loadingOverlay = findViewById(R.id.loadingOverlay)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                if (url.scheme == "mailto") {
                    try {
                        val intent = Intent(Intent.ACTION_SENDTO, url)
                        startActivity(intent)
                    } catch (e: Exception) {
                        Toast.makeText(this@MainActivity, "لا يوجد تطبيق بريد مثبت", Toast.LENGTH_SHORT).show()
                    }
                    return true
                }
                return false
            }

            override fun onPageFinished(view: WebView, url: String?) {
                super.onPageFinished(view, url)
                hideLoadingOverlay()
            }
        }
        webView.addJavascriptInterface(AndroidBridge(), "AndroidBridge")

        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams
            ): Boolean {
                // إلغاء أي طلب سابق لم يكتمل (وإلا يتوقف زر اختيار الملف عن العمل)
                fileChooserCallback?.onReceiveValue(null)
                fileChooserCallback = callback
                return try {
                    // نوع عام */* عمدًا: بعض الهواتف لا تتعرّف على json أو xlsx
                    // فتُخفيها من القائمة - والواجهة تتحقق من صيغة الملف بنفسها
                    val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                        addCategory(Intent.CATEGORY_OPENABLE)
                        type = "*/*"
                    }
                    fileChooserLauncher.launch(Intent.createChooser(intent, "اختر ملفًا"))
                    true
                } catch (e: Exception) {
                    fileChooserCallback = null
                    Toast.makeText(this@MainActivity, "تعذّر فتح اختيار الملفات", Toast.LENGTH_SHORT).show()
                    false
                }
            }
        }

        webView.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            // الملف يأتي من خادم بايثون داخل التطبيق نفسه (127.0.0.1) ولا علاقة
            // له بالإنترنت. تسليمه لأداة تنزيل النظام (DownloadManager) كان يضعه
            // في طابور انتظار "الشبكة"، فلا يظهر تقدّم ولا ينزل الملف إلا عند
            // الاتصال بالإنترنت لاحقًا - أو لا ينزل أبدًا. لذلك نحمّله بأنفسنا
            // مباشرة: العملية محلية وتنتهي في أقل من ثانية بدون أي إنترنت.
            if (isLocalUrl(url)) {
                saveLocalFile(url, mimeType)
            } else {
                enqueueSystemDownload(url, contentDisposition, mimeType)
            }
        }

        val filter = IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(downloadCompleteReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(downloadCompleteReceiver, filter)
        }

        startPythonServer()
        restartContinuousMonitoringServiceIfNeeded()

        Handler(Looper.getMainLooper()).postDelayed({
            webView.loadUrl("http://127.0.0.1:5000/")
        }, 2000)
    }

    /** يُخفي شاشة التحميل الترحيبية بتلاشٍ ناعم بمجرد جاهزية الواجهة فعليًا،
     * بدل ظهور خلفية سوداء/فارغة أثناء إقلاع خادم بايثون. */
    private fun hideLoadingOverlay() {
        if (loadingOverlay.visibility != View.VISIBLE) return
        val fadeOut = AlphaAnimation(1f, 0f).apply { duration = 350 }
        loadingOverlay.startAnimation(fadeOut)
        loadingOverlay.visibility = View.GONE
    }

    private fun isLocalUrl(url: String): Boolean {
        val host = try { Uri.parse(url).host } catch (e: Exception) { null }
        return host == "127.0.0.1" || host == "localhost"
    }

    /** يقرأ الملف من الخادم الداخلي ويحفظه في مجلد التنزيلات بنفسه، ثم يفتح
     * نافذة المشاركة. اسم الملف مأخوذ من آخر جزء في الرابط (تضعه الواجهة هناك
     * عمدًا) لأنه يصل مفكوك الترميز وبالعربية بشكل صحيح. */
    private fun saveLocalFile(url: String, mimeType: String?) {
        Toast.makeText(this, "جارِ حفظ الملف...", Toast.LENGTH_SHORT).show()
        Thread {
            try {
                val connection = (URL(url).openConnection() as HttpURLConnection).apply {
                    connectTimeout = 15000
                    readTimeout = 60000
                    requestMethod = "GET"
                }
                val code = connection.responseCode
                if (code != HttpURLConnection.HTTP_OK) {
                    throw IllegalStateException("الخادم رفض الطلب ($code)")
                }
                val bytes = connection.inputStream.use { it.readBytes() }
                connection.disconnect()

                val fileName = fileNameFromUrl(url, mimeType)
                val type = if (mimeType.isNullOrBlank()) "application/octet-stream" else mimeType
                val uri = writeToDownloads(fileName, type, bytes)

                runOnUiThread {
                    Toast.makeText(this, "حُفظ في مجلد التنزيلات: $fileName", Toast.LENGTH_LONG).show()
                    shareFile(uri, type)
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, "تعذّر حفظ الملف: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    private fun fileNameFromUrl(url: String, mimeType: String?): String {
        val fromPath = try { Uri.parse(url).lastPathSegment } catch (e: Exception) { null }
        val name = if (!fromPath.isNullOrBlank() && fromPath.contains('.')) {
            fromPath
        } else {
            URLUtil.guessFileName(url, null, mimeType)
        }
        // محارف لا تصلح في أسماء الملفات على بعض أنظمة الملفات
        return name.replace(Regex("[\\\\/:*?\"<>|]"), "_")
    }

    /** API 29+ : عبر MediaStore (بدون أي إذن تخزين). أقدم: كتابة مباشرة في
     * مجلد التنزيلات ثم إبلاغ فهرس الوسائط ليظهر الملف في تطبيق الملفات. */
    private fun writeToDownloads(fileName: String, mimeType: String, bytes: ByteArray): Uri {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, fileName)
                put(MediaStore.Downloads.MIME_TYPE, mimeType)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("تعذّر إنشاء الملف في مجلد التنزيلات")
            resolver.openOutputStream(uri)?.use { it.write(bytes) }
                ?: throw IllegalStateException("تعذّرت الكتابة في الملف")
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            resolver.update(uri, values, null, null)
            return uri
        }

        val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        if (!dir.exists()) dir.mkdirs()
        var file = File(dir, fileName)
        if (file.exists()) {
            val dot = fileName.lastIndexOf('.')
            val base = if (dot > 0) fileName.substring(0, dot) else fileName
            val ext = if (dot > 0) fileName.substring(dot) else ""
            var i = 1
            while (file.exists() && i < 1000) {
                file = File(dir, "$base($i)$ext")
                i++
            }
        }
        FileOutputStream(file).use { it.write(bytes) }
        MediaScannerConnection.scanFile(this, arrayOf(file.absolutePath), arrayOf(mimeType), null)
        return FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
    }

    private fun shareFile(uri: Uri, mimeType: String) {
        try {
            val shareIntent = Intent(Intent.ACTION_SEND).apply {
                type = mimeType
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            startActivity(Intent.createChooser(shareIntent, "مشاركة الملف عبر").apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (e: Exception) {
            // المشاركة تحسين إضافي - الملف محفوظ بالفعل في مجلد التنزيلات
        }
    }

    /** مسار احتياطي لأي رابط خارجي (لا يستخدمه التطبيق حاليًا). */
    private fun enqueueSystemDownload(url: String, contentDisposition: String?, mimeType: String?) {
        try {
            val fileName = URLUtil.guessFileName(url, contentDisposition, mimeType)
            val request = DownloadManager.Request(Uri.parse(url))
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName)
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            request.setMimeType(mimeType)
            request.setTitle(fileName)
            val dm = getSystemService(DOWNLOAD_SERVICE) as DownloadManager
            val id = dm.enqueue(request)
            pendingDownloadIds.add(id)
            Toast.makeText(this, "جارِ التنزيل...", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Toast.makeText(this, "تعذّر بدء التنزيل: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun shareDownloadedFile(downloadId: Long) {
        try {
            val dm = getSystemService(DOWNLOAD_SERVICE) as DownloadManager
            val uri = dm.getUriForDownloadedFile(downloadId) ?: return

            val query = DownloadManager.Query().setFilterById(downloadId)
            var mimeType = "*/*"
            dm.query(query)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val mimeIdx = cursor.getColumnIndex(DownloadManager.COLUMN_MEDIA_TYPE)
                    if (mimeIdx >= 0) mimeType = cursor.getString(mimeIdx) ?: mimeType
                }
            }

            val shareIntent = Intent(Intent.ACTION_SEND).apply {
                type = mimeType
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            startActivity(Intent.createChooser(shareIntent, "مشاركة الملف عبر").apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (e: Exception) {
            // فشل المشاركة التلقائية ليس خطأ فادحًا - الملف موجود بالفعل في مجلد التنزيلات
        }
    }

    /** شبكة أمان: لو "وضع المراقبة المستمرة" كان مفعّلاً لكن النظام أوقف الخدمة
     * (نادر لكن ممكن)، نعيد تشغيلها تلقائيًا عند فتح التطبيق. ملاحظة: هذا
     * لا يغطي إعادة التشغيل التلقائي الكامل بعد إعادة تشغيل الهاتف مباشرة
     * (يتطلب ذلك مستقبِل BOOT_COMPLETED منفصل لم يُضَف بعد). */
    private fun restartContinuousMonitoringServiceIfNeeded() {
        if (Prefs.isContinuousMonitoringEnabled(this) && !ContinuousMonitoringService.isRunning) {
            try {
                val intent = Intent(this, ContinuousMonitoringService::class.java)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    startForegroundService(intent)
                } else {
                    startService(intent)
                }
            } catch (e: Exception) {
                // فشل إعادة تشغيل الخدمة لا يجب أن يمنع فتح التطبيق نفسه إطلاقًا -
                // ونصفّر الإعداد حتى لا تتكرر المحاولة الفاشلة في كل مرة يُفتح
                // فيها التطبيق (كان هذا يسبب دخولًا في حلقة تعطّل مستمرة)
                Prefs.setContinuousMonitoringEnabled(this, false)
            }
        }
    }

    private fun startPythonServer() {
        if (PythonServerState.started) return
        PythonServerState.started = true

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        Thread {
            try {
                val py = Python.getInstance()
                val appModule = py.getModule("app")
                appModule.callAttr("start", filesDir.absolutePath)
            } catch (e: PyException) {
                runOnUiThread {
                    Toast.makeText(this, "خطأ في تشغيل خادم بايثون: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    /** الجسر بين واجهة الويب (JavaScript) والوظائف الأصلية في أندرويد */
    inner class AndroidBridge {

        @JavascriptInterface
        fun refreshBackgroundSchedule() {
            runOnUiThread {
                try {
                    Scheduler.refreshBackgroundSchedule(applicationContext)
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "تعذّر تطبيق إعدادات الخلفية: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }

        @JavascriptInterface
        fun requestIgnoreBatteryOptimizations() {
            runOnUiThread {
                val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
                if (!pm.isIgnoringBatteryOptimizations(packageName)) {
                    try {
                        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                        intent.data = Uri.parse("package:$packageName")
                        startActivity(intent)
                    } catch (e: Exception) {
                        Toast.makeText(this@MainActivity, "غير مدعوم على هذا الجهاز", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    Toast.makeText(this@MainActivity, "التطبيق مستثنى بالفعل من توفير البطارية", Toast.LENGTH_SHORT).show()
                }
            }
        }

        @JavascriptInterface
        fun requestNotificationPermission() {
            runOnUiThread {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    val granted = ContextCompat.checkSelfPermission(
                        this@MainActivity, Manifest.permission.POST_NOTIFICATIONS
                    ) == PackageManager.PERMISSION_GRANTED
                    if (!granted) {
                        notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
                }
            }
        }

        @JavascriptInterface
        fun setAppLockEnabled(enabled: Boolean) {
            Prefs.setAppLockEnabled(applicationContext, enabled)
        }

        /** يعرض إشعار نظام حقيقي فورًا - تستدعيها الواجهة (JavaScript) حتى
         * أثناء بقاء التطبيق مفتوحًا في المقدمة، وليس فقط من مهمة الخلفية. */
        @JavascriptInterface
        fun postAlert(message: String) {
            NotificationHelper.postAlert(applicationContext, message)
        }

        /** يشغّل/يوقف "وضع المراقبة المستمرة" (Foreground Service). الأوقات
         * الثابتة تبقى دائمًا على WorkManager العادي بشكل مستقل تمامًا في
         * كل الأحوال (لا تُلغى أو تُعاد جدولتها عند هذا التبديل) - فقط
         * فترة التكرار وكشف الانحراف في الخلفية مرتبطان حصريًا بهذا الوضع. */
        @JavascriptInterface
        fun setContinuousMonitoringEnabled(enabled: Boolean) {
            runOnUiThread {
                try {
                    Prefs.setContinuousMonitoringEnabled(applicationContext, enabled)
                    if (enabled) {
                        val intent = Intent(this@MainActivity, ContinuousMonitoringService::class.java)
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(intent)
                        } else {
                            startService(intent)
                        }
                    } else {
                        stopService(Intent(this@MainActivity, ContinuousMonitoringService::class.java))
                    }
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "تعذّر تفعيل وضع المراقبة المستمرة: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }

        /** يطلب تحديثًا فوريًا لمحتوى إشعار وضع المراقبة المستمرة (بدل
         * انتظار النبضة الدورية القادمة) - تستدعيها الواجهة بعد نجاح أي
         * مزامنة يدوية من داخل التطبيق. لا تأثير لها إن كانت الخدمة متوقفة. */
        @JavascriptInterface
        fun requestNotificationRefresh() {
            runOnUiThread {
                if (!ContinuousMonitoringService.isRunning) return@runOnUiThread
                try {
                    val intent = Intent(this@MainActivity, ContinuousMonitoringService::class.java)
                    intent.action = ContinuousMonitoringService.ACTION_REFRESH_NOW
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        startForegroundService(intent)
                    } else {
                        startService(intent)
                    }
                } catch (e: Exception) {
                    // تجاهل - التحديث الفوري تحسين إضافي وليس أساسيًا
                }
            }
        }
    }

    override fun onDestroy() {
        try {
            unregisterReceiver(downloadCompleteReceiver)
        } catch (e: Exception) {
            // كان غير مسجّل بالفعل
        }
        super.onDestroy()
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
