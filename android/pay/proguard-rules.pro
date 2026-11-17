# Keep names readable in stack traces (lab app, nothing to hide).
-dontobfuscate

# web3j (crypto, RLP and ABI encoding used by the DLT wallet)
-keep class org.web3j.** { *; }
-keep class org.bouncycastle.** { *; }
-dontwarn org.web3j.**
-dontwarn org.bouncycastle.**
-dontwarn com.fasterxml.jackson.**
-dontwarn okhttp3.**
-dontwarn io.reactivex.**
-dontwarn java.lang.management.**
-dontwarn javax.naming.**
# Please add these rules to your existing keep rules in order to suppress warnings.
# This is generated automatically by the Android Gradle plugin.
-dontwarn org.slf4j.impl.StaticLoggerBinder