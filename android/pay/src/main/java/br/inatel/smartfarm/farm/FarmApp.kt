package br.inatel.smartfarm.farm

import android.app.Application
import android.os.SystemClock
import br.inatel.smartfarm.farm.core.FarmController
import br.inatel.smartfarm.farm.protocol.WallClock

class FarmApp : Application() {
    lateinit var controller: FarmController
        private set

    override fun onCreate() {
        super.onCreate()
        WallClock.install(SystemClock::elapsedRealtimeNanos)
        controller = FarmController(this)
    }
}
