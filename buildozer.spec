[app]
title = MEOE Overlay
package.name = meoe
package.domain = com.meoe
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,ttf
version = 1.0

requirements = python3,kivy==2.2.1,android,androidstorage4kivy,numpy

orientation = landscape
fullscreen = 0

android.permissions = SYSTEM_ALERT_WINDOW,FOREGROUND_SERVICE,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,INTERNET
android.api = 33
android.minapi = 21
android.ndk = 25b
android.archs = arm64-v8a
android.allow_backup = true
android.meta_data = android.permission.SYSTEM_ALERT_WINDOW=true
android.no-compile-pyo = True

[buildozer]
log_level = 2
warn_on_root = 0
