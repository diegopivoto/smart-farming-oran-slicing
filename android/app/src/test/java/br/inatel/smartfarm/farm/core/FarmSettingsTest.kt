package br.inatel.smartfarm.farm.core

import br.inatel.smartfarm.farm.protocol.Orchestration
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], application = android.app.Application::class)
class FarmSettingsTest {
    @Test fun oranModeDefaultsToTheRanDefaultsAndPersists() {
        val app = RuntimeEnvironment.getApplication()
        val s = FarmSettings(app)
        s.restoreDefaults()
        assertEquals(Orchestration.ORAN_RAN_DEFAULT, s.oranMode)
        s.oranMode = Orchestration.ORAN_DDQN
        assertEquals(Orchestration.ORAN_DDQN, FarmSettings(app).oranMode) // persisted like the other settings
        s.oranMode = "turbo"
        assertEquals(Orchestration.ORAN_RAN_DEFAULT, FarmSettings(app).oranMode)
        s.oranMode = Orchestration.ORAN_DDQN
        s.restoreDefaults()
        assertEquals(Orchestration.ORAN_RAN_DEFAULT, FarmSettings(app).oranMode)
    }
}
