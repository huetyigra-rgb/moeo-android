[app]

title = MEOE Overlay

package.name = meoe
package.domain = com.meoe

source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,json,ttf

version = 1.0

requirements = python3,kivy==2.2.1,pyjnius,androidstorage4kivy

orientation = landscape

fullscreen = 0

android.api = 33
android.minapi = 24
android.ndk = 25b

android.archs = arm64-v8a

android.permissions = INTERNET

icon.filename = icon.png

android.allow_backup = True

[buildozer]

log_level = 2
warn_on_root = 0
