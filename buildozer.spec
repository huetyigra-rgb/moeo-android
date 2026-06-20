name: Build MEOE APK

on:
  push:
    branches: [main, master]
  workflow_dispatch:

jobs:
  build-android:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Setup Python 3.10
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Setup Java 17
        uses: actions/setup-java@v4
        with:
          distribution: 'temurin'
          java-version: '17'

      - name: Install system dependencies
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y \
            python3-pip build-essential git \
            ffmpeg libsdl2-dev libsdl2-image-dev \
            libsdl2-mixer-dev libsdl2-ttf-dev \
            libportmidi-dev libswscale-dev \
            libavformat-dev libavcodec-dev \
            zlib1g-dev libgstreamer1.0 \
            gstreamer1.0-plugins-base \
            gstreamer1.0-plugins-good \
            libsqlite3-dev libffi-dev \
            libssl-dev autoconf automake \
            libtool pkg-config \
            aidl

      - name: Install Buildozer and Cython
        run: |
          pip install --upgrade pip
          pip install buildozer cython

      - name: Setup Android SDK in Buildozer path
        run: |
          BSDK=$HOME/.buildozer/android/platform/android-sdk
          mkdir -p $BSDK/cmdline-tools

          wget -q https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip
          unzip -q commandlinetools-linux-9477386_latest.zip -d $BSDK/cmdline-tools
          mv $BSDK/cmdline-tools/cmdline-tools $BSDK/cmdline-tools/latest

          export SDKMANAGER=$BSDK/cmdline-tools/latest/bin/sdkmanager

          # Accept licences
          yes | $SDKMANAGER --sdk_root=$BSDK --licenses > /dev/null 2>&1

          # Install required components
          $SDKMANAGER --sdk_root=$BSDK \
            "platform-tools" \
            "platforms;android-33" \
            "build-tools;33.0.2"

          # --- KEY FIX ---
          # Buildozer looks for sdkmanager at the LEGACY path: tools/bin/sdkmanager
          # Create that path and symlink everything there
          mkdir -p $BSDK/tools/bin
          ln -sf $BSDK/cmdline-tools/latest/bin/sdkmanager $BSDK/tools/bin/sdkmanager
          ln -sf $BSDK/cmdline-tools/latest/bin/avdmanager  $BSDK/tools/bin/avdmanager

          # Symlink system aidl into build-tools
          ln -sf $(which aidl) $BSDK/build-tools/33.0.2/aidl

          echo "ANDROIDSDK=$BSDK" >> $GITHUB_ENV
          echo "$BSDK/cmdline-tools/latest/bin" >> $GITHUB_PATH
          echo "$BSDK/platform-tools"           >> $GITHUB_PATH
          echo "$BSDK/build-tools/33.0.2"       >> $GITHUB_PATH

      - name: Build APK
        run: |
          buildozer android debug
        timeout-minutes: 120

      - name: Upload APK artifact
        uses: actions/upload-artifact@v4
        with:
          name: meoe-apk
          path: bin/*.apk
          retention-days: 30
